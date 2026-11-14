"""多人模式契約 v1 的 GPU 這一半：成品、請求與 /capabilities 都要和範例一致。

範例檔（fixtures/speaker_contract_v1.json）與新聞服務那邊的副本逐位元組相同。兩邊各自用它驗證
自己的那一半，所以改任何欄位都要兩邊一起改、一起升 speaker_contract 的版本。
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from slidebox.domain.entities import DeckResult, Settings
from slidebox.usecases import speakers
from slidebox.usecases.conversation import render_slide
from slidebox_api.app import CapabilitiesView, JobRequest, create_app
from slidebox_api.jobs import JobStore, SpeakerMode
from slidebox_api.payload import SPEAKER_CONTRACT, deck_payload
from slidebox_api.runner import JobWorker, SlideboxExecutor
from tests.slidebox import diarized_support as support

from .speaker_support import deck_from_result, key_paths, load_contract, violations

CONTRACT = load_contract()
RESULT = CONTRACT["result"]


def test_the_example_is_what_deck_payload_makes_of_the_deck_it_describes():
    assert deck_payload(deck_from_result(RESULT), "abcdefghijk") == RESULT


def test_the_example_survives_a_trip_through_json_unchanged():
    payload = deck_payload(deck_from_result(RESULT), "abcdefghijk")
    assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload


def test_the_text_of_every_slide_in_the_example_is_what_the_pipeline_renders_from_its_points():
    """攔的 bug：條列、詳述或時間的組法改了，範例（以及新聞服務照它寫的程式）卻沒跟上。"""
    deck = deck_from_result(RESULT)
    for slide in deck.slides:
        rendered = render_slide(slide.index, slide.title, slide.points)
        assert rendered.bullets == slide.bullets
        assert rendered.detail == slide.detail
        assert rendered.timestamp == slide.timestamp


def test_the_example_passes_every_check_the_news_service_makes_before_it_saves():
    assert violations(RESULT, requested_speakers=None) == []


def test_the_version_the_example_declares_is_the_one_the_code_announces():
    assert CONTRACT["capabilities"]["speaker_contract"] == SPEAKER_CONTRACT == 1


def test_the_request_example_is_a_valid_request():
    request = JobRequest(**CONTRACT["request"])
    assert request.speaker_mode == SpeakerMode.DIARIZE
    assert request.speaker_count is None
    assert request.detailed is True and (request.min_slides, request.max_slides) == (2, 6)
    assert request.speech_hint == CONTRACT["request"]["speech_hint"]


# v1 之後疊上去的能力旗標：舊的新聞服務不認得它、不看它，所以不升 speaker_contract 的版本
ADDED_CAPABILITIES = {"video_range"}


def _says_what_the_example_says(body: dict) -> bool:
    """範例裡的每一個鍵都在、值一樣；多出來的只能是上面登記過的疊上去的旗標。"""
    return ({key: body[key] for key in CONTRACT["capabilities"]} == CONTRACT["capabilities"]
            and set(body) - set(CONTRACT["capabilities"]) == ADDED_CAPABILITIES)


def test_the_capabilities_example_is_a_valid_answer():
    assert _says_what_the_example_says(CapabilitiesView(**CONTRACT["capabilities"]).model_dump())


def _client(probe):
    store = JobStore()
    worker = JobWorker(store, lambda job, progress, cancelled: {})
    app = create_app(store=store, worker=worker, api_key="", model="m", ollama_host="http://h",
                     probe_ollama=lambda: True, probe_speaker_diarization=probe)
    return TestClient(app), store, worker


def test_the_capabilities_endpoint_says_exactly_what_the_example_says_for_a_ready_gpu():
    client, _, worker = _client(lambda: (True, ""))
    with client:
        worker.stop()
        assert _says_what_the_example_says(client.get("/capabilities").json())


def test_the_request_example_is_accepted_over_http_and_reaches_the_job_unchanged():
    client, store, worker = _client(lambda: (True, ""))
    with client:
        worker.stop()
        response = client.post("/jobs", json=CONTRACT["request"])
    assert response.status_code == 202
    job = store.get(response.json()["id"])
    request = CONTRACT["request"]
    assert (job.url, job.detailed, job.min_slides, job.max_slides, job.speech_hint) == (
        request["url"], request["detailed"], request["min_slides"], request["max_slides"],
        request["speech_hint"])
    assert (job.speaker_mode, job.speaker_count) == ("diarize", None)


class _CannedUseCase:
    def __init__(self, deck):
        self._deck = deck
        self.calls: list[dict] = []

    def execute(self, url, settings, progress=None, is_cancelled=None, **kwargs):
        self.calls.append(dict(url=url, **kwargs))
        return DeckResult(self._deck, "OUT/x.html")


def test_the_whole_way_from_the_request_example_to_the_result_example():
    """請求 → 工作 → 執行函式 → 成品：端到端各接一次，用範例當輸入與期望。"""
    store = JobStore()
    usecase = _CannedUseCase(deck_from_result(RESULT))
    executor = SlideboxExecutor(usecase, Settings(output_dir="OUT"))
    request = JobRequest(**CONTRACT["request"])
    job = store.submit(
        request.url, detailed=request.detailed, min_slides=request.min_slides,
        max_slides=request.max_slides, speech_hint=request.speech_hint,
        speaker_mode=request.speaker_mode, speaker_count=request.speaker_count)

    payload = executor(job, lambda fraction, status: None, lambda: False)

    assert payload == RESULT
    assert usecase.calls == [{"url": request.url, "speech_hint": request.speech_hint,
                              "speaker_mode": SpeakerMode.DIARIZE, "speaker_count": None}]


def test_a_payload_the_pipeline_really_makes_has_the_same_shape_as_the_example():
    """攔的 bug：use case 或 payload 多了／少了／改名一個欄位，範例卻沒改——兩邊的契約就悄悄分家。"""
    parts = support.make()
    deck = support.run(parts).deck

    payload = deck_payload(deck, "abcdefghijk")

    assert key_paths(payload) == key_paths(RESULT)
    assert violations(payload, requested_speakers=None) == []
    assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload


def test_a_payload_with_exactly_twenty_four_speakers_passes_the_checks_and_a_twenty_fifth_would_not():
    """攔的 bug：GPU 的上限和新聞服務的上限（speakers 1 到 24 個）差一個，或差在有沒有算其他聲音。
    正好 24 個（2 位主要發言者、22 個其他聲音）要過；多一個，新聞服務一定擋，GPU 也一定不交。"""
    payload = deck_payload(support.run(support.crowd_parts(21)).deck, "abcdefghijk")

    assert len(payload["speakers"]) == 24
    assert key_paths(payload) == key_paths(RESULT)
    assert violations(payload, requested_speakers=None) == []
    assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload

    twenty_fifth = copy.deepcopy(payload)
    twenty_fifth["speakers"].append({**twenty_fifth["speakers"][-1], "id": "S25"})
    assert "speakers 不是 1 到 24 個" in violations(twenty_fifth, requested_speakers=None)


# --- 三條線各寫死一份的上限，不能悄悄分家 ---

REPO_ROOT = Path(__file__).resolve().parents[2]
PI_CHECK = REPO_ROOT / "services" / "news" / "articles" / "speaker_result.py"
SITE_LIB = REPO_ROOT / "web" / "news" / "src" / "lib" / "speakers.ts"


def constant_in(path: Path, pattern: str) -> int:
    """另一條線的原始碼裡寫死的一個整數。只看行首的常數定義，註解裡提到的數字不算；找不到就是失敗
    （常數改名了，這個檢查就要跟著改，不能悄悄變成什麼都沒比）。"""
    found = re.search(pattern, path.read_text("utf-8"), re.MULTILINE)
    assert found, f"{path.name} 裡找不到符合 {pattern} 的常數定義"
    return int(found.group(1))


def test_the_constant_finder_reads_the_definition_and_not_a_comment_about_it(tmp_path):
    source = tmp_path / "limits.py"
    source.write_text("# MAX_SPEAKERS = 99（舊的數字）\nMAX_SPEAKERS = 24    # 上限\nMAX_SPEAKERS_OPTION = 20\n", "utf-8")
    assert constant_in(source, r"^MAX_SPEAKERS = (\d+)") == 24
    with pytest.raises(AssertionError, match="找不到"):
        constant_in(source, r"^MAX_PARTICIPANTS = (\d+)")


@pytest.mark.skipif(not (PI_CHECK.exists() and SITE_LIB.exists()),
                    reason="新聞服務與網站的程式不在這個 checkout 裡（只有 GPU 這條線的 image）")
def test_the_speaker_limits_are_the_same_numbers_on_all_three_sides():
    """攔的 bug：只改了一邊。GPU 的上限比新聞服務大，GPU 做完整支影片、成品交過去才被拒絕；比它小，新聞服務
    收得下的結果 GPU 先擋掉了。網站的卡片人數用同一個契約數字，頁面的 DOM 上限也要留得下全部的說話者。"""
    assert constant_in(PI_CHECK, r"^MAX_SPEAKERS = (\d+)") == speakers.MAX_PUBLISHED_SPEAKERS
    assert constant_in(PI_CHECK, r"^MAX_PARTICIPANTS = (\d+)") == speakers.MAX_PARTICIPANTS
    assert constant_in(SITE_LIB, r"^const CONTRACT_MAX_SPEAKERS = (\d+);") == speakers.MAX_PUBLISHED_SPEAKERS
    assert constant_in(SITE_LIB, r"^const MAX_SPEAKERS = (\d+);") >= speakers.MAX_PUBLISHED_SPEAKERS


def test_a_fixed_speaker_count_comes_back_as_requested_speakers():
    parts = support.make()
    payload = deck_payload(support.run(parts, speaker_count=3).deck, "abcdefghijk")
    assert payload["diarization"]["requested_speakers"] == 3
    assert violations(payload, requested_speakers=3) == []
    assert violations(payload, requested_speakers=None) != []


# --- 檢查本身要有牙齒：下面每一種壞掉的成品都要被抓到 ---


def _broken(change):
    payload = copy.deepcopy(RESULT)
    change(payload)
    return payload


BREAKAGES = {
    "not diarize": lambda p: p.update(speaker_mode="single"),
    "a brief is present": lambda p: p.update(brief={"one_liner": "x", "key_numbers": [], "asks": []}),
    "no diarization report": lambda p: p.pop("diarization"),
    "unknown status": lambda p: p["diarization"].update(status="fine"),
    "no speakers": lambda p: p.update(speakers=[]),
    "duplicate speaker ids": lambda p: p["speakers"][1].update(id="S1"),
    "malformed speaker id": lambda p: p["speakers"][1].update(id="speaker2"),
    "unknown speaker kind": lambda p: p["speakers"][0].update(kind="host"),
    "nobody is a participant": lambda p: [s.update(kind="minor") for s in p["speakers"]],
    "a slide without points": lambda p: p["slides"][1].update(points=[]),
    "a point given to a minor voice": lambda p: p["slides"][0]["points"][0].update(speaker="S3"),
    "a point given to nobody listed": lambda p: p["slides"][0]["points"][0].update(speaker="S9"),
    "negative turn": lambda p: p["slides"][0]["points"][0].update(turn=-1),
    "fractional turn": lambda p: p["slides"][0]["points"][0].update(turn=1.5),
    "start is not a number": lambda p: p["slides"][0]["points"][0].update(start=float("nan")),
    "empty point text": lambda p: p["slides"][0]["points"][0].update(text=""),
    "a code inside the point text": lambda p: p["slides"][0]["points"][0].update(text="[[S2]]：代號"),
    "evidence far too long": lambda p: p["slides"][0]["points"][0].update(evidence="字" * 121),
    "a bullet names someone unlisted": lambda p: p["slides"][0]["bullets"].append("[[S7]]：誰"),
    "a transcript line without a speaker": lambda p: p.update(transcript_text="00:05 沒有說話者"),
    "no transcript": lambda p: p.update(transcript_text=""),
}


@pytest.mark.parametrize("name", sorted(BREAKAGES))
def test_the_checks_notice_every_kind_of_broken_result(name):
    assert violations(_broken(BREAKAGES[name]), requested_speakers=None) != []


def test_the_checks_notice_a_wrong_requested_count():
    assert violations(RESULT, requested_speakers=2) != []
