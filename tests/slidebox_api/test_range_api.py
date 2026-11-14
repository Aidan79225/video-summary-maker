"""只處理影片的一段區間（range_start／range_end）：HTTP 請求規則、工作、執行函式、成品與能力旗標。

全部用假的 use case：不碰 Ollama、GPU、ffmpeg 或網路。區間怎麼切、怎麼把時間加回原影片的時間軸，
在 tests/slidebox/test_build_deck_range.py。
"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from slidebox.domain.entities import TimeRange
from slidebox_api.jobs import Job, JobStore, SpeakerMode
from slidebox_api.payload import deck_payload

from .speaker_support import (
    FOLLOWUP,
    TOPIC,
    YOUTUBE,
    CannedUseCase,
    deck_from_result,
    finish,
    key_paths,
    kit,
    load_contract,
    submit,
)

CONTRACT = load_contract()
RESULT = CONTRACT["result"]
DIARIZE = {"url": YOUTUBE, "speaker_mode": "diarize"}


def ranged_deck(start=600.0, end=1500.0):
    """契約範例的成品，說話者區分報告多帶上這次實際處理的區間。"""
    deck = deck_from_result(RESULT)
    return replace(deck, diarization=replace(deck.diarization, range_start=start, range_end=end))


# --- /capabilities：新聞服務靠這個旗標才敢送區間 ---


@pytest.mark.parametrize("probe", [lambda: (True, ""), lambda: (False, "沒有設定 token"), None],
                         ids=["ready", "cannot diarize", "no probe"])
def test_a_gpu_that_knows_ranges_says_so_whatever_state_the_diarizer_is_in(probe):
    """旗標說的是「這個版本看得懂 range_start／range_end」，跟聲音區分現在能不能用是兩回事。
    舊版的 GPU 沒有這個欄位，而它的請求模型不擋多出來的欄位：新聞服務要看到 true 才送區間，
    否則舊版會默默處理整支影片。"""
    client, *_ = kit(probe=probe)
    with client:
        assert client.get("/capabilities").json()["video_range"] is True


def test_the_flag_is_still_there_when_the_probe_blows_up():
    def broken():
        raise RuntimeError("載入失敗")

    client, *_ = kit(probe=broken)
    with client:
        body = client.get("/capabilities").json()
    assert body["speaker_diarization"] is False and body["video_range"] is True


def test_the_contract_version_does_not_change_because_of_the_flag():
    """旗標與結果裡的兩個鍵都是疊上去的，舊的新聞服務照樣看得懂：版本還是 1。"""
    client, *_ = kit(probe=lambda: (True, ""))
    with client:
        assert client.get("/capabilities").json()["speaker_contract"] == 1


# --- 請求規則 ---


@pytest.mark.parametrize("fields,expected", [
    ({}, (None, None)),
    ({"range_start": None, "range_end": None}, (None, None)),
    ({"range_start": 600, "range_end": 1500}, (600.0, 1500.0)),
    ({"range_start": 600}, (600.0, None)),                 # 只給起點：一直到影片結尾
    ({"range_end": 1500}, (None, 1500.0)),                 # 只給終點：從頭開始
    ({"range_start": 0, "range_end": 5}, (0.0, 5.0)),      # 剛好是最短的區間
    ({"range_start": 61.5, "range_end": 120.25}, (61.5, 120.25)),
    ({"range_start": 0}, (0.0, None)),                     # 等於整支影片：由 use case 當成沒有區間
])
def test_the_allowed_ranges_reach_the_job(fields, expected):
    response, store, *_ = submit({**DIARIZE, **fields})
    assert response.status_code == 202
    job = store.get(response.json()["id"])
    assert (job.range_start, job.range_end) == expected


@pytest.mark.parametrize("fields", [
    {"range_start": -1},
    {"range_start": -0.5, "range_end": 100},
    {"range_start": 600, "range_end": 600},                # 終點沒有比起點晚
    {"range_start": 600, "range_end": 300},
    {"range_start": 600, "range_end": 604},                # 不到 5 秒
    {"range_start": 600, "range_end": 604.9},
    {"range_end": 4},                                      # 只給終點也不能不到 5 秒
    {"range_end": 0},
    {"range_end": -10},
    {"range_start": "十分鐘"},
    {"range_end": "到結尾"},
    {"range_start": [600]},
])
def test_a_range_that_makes_no_sense_is_refused_before_any_job_exists(fields):
    response, store, *_ = submit({**DIARIZE, **fields})
    assert response.status_code == 422
    assert store.counts() == (0, 0)


def test_a_range_that_is_too_short_says_how_short_it_may_be():
    response, *_ = submit({**DIARIZE, "range_start": 600, "range_end": 603})
    assert response.status_code == 422
    assert "至少 5 秒" in response.json()["detail"]


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("field", ["range_start", "range_end"])
def test_a_time_that_is_not_a_finite_number_is_refused(field, literal):
    """JSON 的標準沒有 NaN／Infinity，但 Python 的 json 照樣吃：不能讓它們一路走到 ffmpeg 的 -ss。"""
    client, store, worker, _ = kit()
    raw = '{"url": "%s", "speaker_mode": "diarize", "%s": %s}' % (YOUTUBE, field, literal)
    with client:
        worker.stop()
        response = client.post("/jobs", content=raw, headers={"content-type": "application/json"})
    assert response.status_code == 422
    assert store.counts() == (0, 0)


@pytest.mark.parametrize("fields", [
    {"range_start": 600},
    {"range_end": 1500},
    {"range_start": 600, "range_end": 1500},
    {"speaker_mode": "single", "range_start": 600, "range_end": 1500},
    {"speaker_mode": "single", "speaker_count": 1, "range_start": 600, "range_end": 1500},
    {"range_start": 0},
])
def test_a_range_without_the_multi_speaker_mode_is_refused_not_ignored(fields):
    """單人（議會）的 pipeline 不切區間：默默處理整支影片的話，送的人會以為只處理了那一段。"""
    response, store, *_ = submit({"url": YOUTUBE, **fields})
    assert response.status_code == 422
    assert "diarize" in response.json()["detail"]
    assert store.counts() == (0, 0)


@pytest.mark.parametrize("base", [TOPIC, FOLLOWUP], ids=["topic", "followup"])
@pytest.mark.parametrize("fields", [
    {"range_start": 600},
    {"range_end": 1500},
    {"range_start": 600, "range_end": 1500},
    {"range_start": 0},
])
def test_a_range_on_a_job_that_has_no_video_is_refused_not_ignored(base, fields):
    response, store, *_ = submit({**base, **fields})
    assert response.status_code == 422
    assert store.counts() == (0, 0)


@pytest.mark.parametrize("base", [TOPIC, FOLLOWUP], ids=["topic", "followup"])
def test_explicit_nulls_stay_acceptable_on_every_kind_of_job(base):
    response, *_ = submit({**base, "range_start": None, "range_end": None})
    assert response.status_code == 202


# --- 工作 ---


def test_a_job_built_without_a_range_covers_the_whole_video():
    """攔的 bug：Job 的預設值改成別的，沒帶欄位的呼叫端（議會、舊的新聞服務）就會悄悄只處理一段。"""
    assert (Job(id="x", url="u").range_start, Job(id="x", url="u").range_end) == (None, None)
    submitted = JobStore().submit("u")
    assert (submitted.range_start, submitted.range_end) == (None, None)


def test_a_submitted_range_is_kept_on_the_job_and_on_every_snapshot_of_it():
    store = JobStore()
    job = store.submit(YOUTUBE, speaker_mode=SpeakerMode.DIARIZE, range_start=600.0, range_end=1500.0)
    for seen in (job, store.snapshot(job.id), store.recent_snapshots()[0]):
        assert (seen.range_start, seen.range_end) == (600.0, 1500.0)


# --- 執行：use case 被怎麼叫 ---


def test_a_ranged_job_tells_the_use_case_the_range():
    view, usecase = finish({**DIARIZE, "range_start": 600, "range_end": 1500})

    assert view["status"] == "done"
    assert usecase.calls == [{"url": YOUTUBE, "speech_hint": None, "speaker_mode": "diarize",
                              "speaker_count": None, "time_range": TimeRange(600.0, 1500.0)}]


@pytest.mark.parametrize("fields,expected", [
    ({"range_start": 600}, TimeRange(600.0, None)),
    ({"range_end": 1500}, TimeRange(0.0, 1500.0)),
    ({"range_start": 0}, TimeRange(0.0, None)),
    ({"range_start": 0, "range_end": 90}, TimeRange(0.0, 90.0)),
])
def test_a_half_open_range_is_filled_in_for_the_use_case(fields, expected):
    _, usecase = finish({**DIARIZE, **fields})
    assert usecase.calls[0]["time_range"] == expected


def test_a_fixed_speaker_count_and_a_range_travel_together():
    _, usecase = finish({**DIARIZE, "speaker_count": 3, "range_start": 600, "range_end": 1500})
    call = usecase.calls[0]
    assert (call["speaker_mode"], call["speaker_count"], call["time_range"]) == (
        "diarize", 3, TimeRange(600.0, 1500.0))


@pytest.mark.parametrize("body", [DIARIZE, CONTRACT["request"], {**DIARIZE, "speaker_count": 2}],
                         ids=["plain", "contract example", "fixed count"])
def test_a_job_without_a_range_does_not_mention_one_to_the_use_case(body):
    """攔的 bug：沒有區間的工作也多帶一個 time_range=None——議會與舊 client 的呼叫就和沒有這個功能
    時不一樣了（use case 的簽章、假的 use case 與錄下來的呼叫都會跟著變）。"""
    _, usecase = finish(body)
    assert "time_range" not in usecase.calls[0]


# --- 成品 ---


def test_the_range_the_use_case_reports_goes_into_the_diarization_block_as_it_is():
    diarization = deck_payload(ranged_deck(600.0, 1500.0), "abcdefghijk")["diarization"]
    assert (diarization["range_start"], diarization["range_end"]) == (600.0, 1500.0)


def test_without_a_range_the_result_is_exactly_what_it_always_was():
    """攔的 bug：沒有區間的成品也多出 range_start: null 之類的鍵——契約範例與舊的新聞服務的驗證就會壞。"""
    payload = deck_payload(deck_from_result(RESULT), "abcdefghijk")
    assert payload == RESULT
    assert "range_start" not in payload["diarization"] and "range_end" not in payload["diarization"]


def test_a_ranged_result_adds_exactly_two_keys_to_the_contract_shape_and_stays_plain_json():
    payload = deck_payload(ranged_deck(), "abcdefghijk")
    assert key_paths(payload) - key_paths(RESULT) == {"diarization.range_start", "diarization.range_end"}
    assert key_paths(RESULT) - key_paths(payload) == set()
    assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload


@pytest.mark.parametrize("start,end", [(600.0, None), (None, 1500.0)])
def test_a_half_reported_range_is_not_delivered(start, end):
    """use case 保證兩端一起回報（只給起點時終點用影片長度）。哪天破了，工作要失敗，
    而不是交出一份新聞服務一定會擋下的成品。"""
    with pytest.raises(ValueError, match="區間"):
        deck_payload(ranged_deck(start, end), "abcdefghijk")


def test_a_ranged_request_comes_back_with_the_range_and_nothing_else_changed():
    body = {**CONTRACT["request"], "range_start": 600, "range_end": 1500}

    view, usecase = finish(body, usecase=CannedUseCase(deck=ranged_deck(600.0, 1500.0)))

    assert view["status"] == "done" and view["error"] == ""
    assert usecase.calls[0]["time_range"] == TimeRange(600.0, 1500.0)
    diarization = dict(view["result"]["diarization"])
    assert (diarization.pop("range_start"), diarization.pop("range_end")) == (600.0, 1500.0)
    assert diarization == RESULT["diarization"]
    assert {k: v for k, v in view["result"].items() if k != "diarization"} == {
        k: v for k, v in RESULT.items() if k != "diarization"}
