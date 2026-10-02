"""HTTP 介面：用假的執行函式，不碰 Ollama 也不碰網路。"""
from __future__ import annotations

import io
import json
import threading
from functools import partial

import pytest
from fastapi.testclient import TestClient

from slidebox.domain.entities import FollowUpPair, TopicLabel
from slidebox.domain.errors import OperationCancelled
from slidebox.infrastructure import ollama_summarizer
from slidebox.infrastructure.ollama_summarizer import OllamaFollowUpJudge
from slidebox.usecases.followups import classifier_name as followup_classifier
from slidebox_api.app import create_app
from slidebox_api.jobs import JobKind, JobStatus, JobStore
from slidebox_api.runner import ByKindExecutor, FollowUpExecutor, JobWorker

IVOD = "https://ivod.ly.gov.tw/Play/Clip/1M/171180"


class FakeExecutor:
    """可以停在閘門前的假執行函式，用來觀察「執行中」的狀態。"""

    def __init__(self, error=None):
        self.gate = threading.Event()
        self.gate.set()
        self.calls: list = []
        self.error = error

    def __call__(self, job, progress, is_cancelled):
        self.calls.append(job)
        progress(0.4, "產生摘要…")
        while not self.gate.wait(0.01):
            if is_cancelled():
                raise OperationCancelled()
        if self.error is not None:
            raise self.error
        return {"video_id": "171180", "slides": [], "title": job.url}


def _app(store, worker, api_key="", reachable=True):
    return create_app(store=store, worker=worker, api_key=api_key,
                      model="qwen3.5:9b", ollama_host="http://localhost:11434",
                      probe_ollama=lambda: reachable)


@pytest.fixture
def kit():
    store = JobStore()
    executor = FakeExecutor()
    worker = JobWorker(store, executor)
    # 不啟動背景執行緒：測試自己決定何時跑，結果才是確定的
    with TestClient(_app(store, worker)) as client:
        worker.stop()
        yield client, store, worker, executor


def test_health_answers_even_when_nothing_has_been_submitted(kit):
    client, *_ = kit
    body = client.get("/health").json()
    assert body["ok"] is True
    assert "model" in body and "ollama_reachable" in body


def test_submitting_returns_202_and_a_job_id(kit):
    client, *_ = kit
    response = client.post("/jobs", json={"url": IVOD})
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["id"]
    # 新聞用途一律詳細模式
    assert body["detailed"] is True


def test_the_result_appears_once_the_job_has_run(kit):
    client, store, worker, _ = kit
    job_id = client.post("/jobs", json={"url": IVOD}).json()["id"]
    assert worker.run_once() is True
    body = client.get(f"/jobs/{job_id}").json()
    assert body["status"] == "done"
    assert body["result"]["video_id"] == "171180"


def test_a_failure_is_reported_with_its_reason(kit):
    client, store, worker, executor = kit
    executor.error = RuntimeError("連不上 Ollama")
    job_id = client.post("/jobs", json={"url": IVOD}).json()["id"]
    worker.run_once()
    body = client.get(f"/jobs/{job_id}").json()
    assert body["status"] == "failed"
    assert "Ollama" in body["error"]


def test_the_listing_leaves_out_the_results(kit):
    """攔的 bug：每篇成品帶著幾百 KB 的 base64 圖片，清單全帶會讓
    「看看有哪些工作」變成幾十 MB 的回應。"""
    client, store, worker, _ = kit
    client.post("/jobs", json={"url": IVOD})
    worker.run_once()
    items = client.get("/jobs").json()
    assert items[0]["status"] == "done"
    assert items[0]["result"] is None


def test_an_unknown_job_is_404(kit):
    client, *_ = kit
    assert client.get("/jobs/沒這個").status_code == 404
    assert client.delete("/jobs/沒這個").status_code == 404


def test_a_queued_job_can_be_cancelled_before_it_ever_runs(kit):
    client, store, worker, executor = kit
    job_id = client.post("/jobs", json={"url": IVOD}).json()["id"]
    assert client.delete(f"/jobs/{job_id}").json()["status"] == JobStatus.CANCELLED
    worker.run_once()
    assert executor.calls == []


def test_cancelling_a_running_job_stops_it(kit):
    """攔的 bug：取消只改狀態，執行緒還在跑、GPU 還被佔著。"""
    client, store, worker, executor = kit
    executor.gate.clear()
    job_id = client.post("/jobs", json={"url": IVOD}).json()["id"]
    thread = threading.Thread(target=worker.run_once)
    thread.start()
    try:
        _wait(lambda: store.get(job_id).status == JobStatus.RUNNING)
        client.delete(f"/jobs/{job_id}")
        _wait(lambda: store.get(job_id).status == JobStatus.CANCELLED)
    finally:
        executor.gate.set()
        thread.join(5)
    assert client.get(f"/jobs/{job_id}").json()["status"] == JobStatus.CANCELLED


def test_an_impossible_slide_range_is_refused(kit):
    client, *_ = kit
    response = client.post("/jobs", json={"url": IVOD, "min_slides": 9, "max_slides": 3})
    assert response.status_code == 422


@pytest.mark.parametrize("url", ["", "file:///C:/Windows/win.ini", "ftp://x/y",
                                 "not-a-url"])
def test_a_url_that_is_not_http_is_refused(kit, url):
    """攔的 bug：這個網址會被交給 yt-dlp。沒有限定 scheme 的話，任何連得到
    這個埠的人都能用 file:// 讀 GPU 主機上的本機檔案。"""
    client, *_ = kit
    assert client.post("/jobs", json={"url": url}).status_code == 422


def test_the_api_key_is_enforced_when_one_is_configured():
    store = JobStore()
    worker = JobWorker(store, FakeExecutor())
    with TestClient(_app(store, worker, api_key="secret")) as client:
        worker.stop()
        assert client.post("/jobs", json={"url": IVOD}).status_code == 401
        ok = client.post("/jobs", json={"url": IVOD}, headers={"X-API-Key": "secret"})
        assert ok.status_code == 202
        # 健康檢查不需要金鑰：監控與反向代理要能探活
        assert client.get("/health").status_code == 200


def _wait(predicate, timeout=5.0):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    raise AssertionError("等不到預期的狀態")


def test_a_non_ascii_api_key_is_rejected_not_a_crash():
    """攔的 bug：compare_digest 對非 ASCII 的 str 會丟 TypeError，於是一個
    亂填的金鑰變成 500 而不是 401。

    header 在線路上是 latin-1，所以真正送得進來的非 ASCII 是這個範圍。
    """
    store = JobStore()
    worker = JobWorker(store, FakeExecutor())
    with TestClient(_app(store, worker, api_key="secret")) as client:
        worker.stop()
        response = client.post("/jobs", json={"url": IVOD},
                               headers={"X-API-Key": "sécret".encode("latin-1")})
        assert response.status_code == 401


def test_a_speech_hint_is_stored_on_the_job_and_capped(kit):
    client, store, _, _ = kit
    job_id = client.post("/jobs", json={"url": IVOD, "speech_hint": "臺中市議會。發言者：楊啓邦"}).json()["id"]
    assert store.get(job_id).speech_hint == "臺中市議會。發言者：楊啓邦"
    assert client.post("/jobs", json={"url": IVOD, "speech_hint": "x" * 201}).status_code == 422


# --- 分類工作（kind=topic）---

LABELS = [
    {"key": "defense", "label": "國防外交", "description": "國防、軍事、外交、兩岸、僑務"},
    {"key": "welfare", "label": "衛生福利", "description": "醫療、健保、長照、社福、托育、食安"},
]
TEXT = "一句話：要求衛福部說明長照 3.0 的預算\n各段小標：\n- 長照人力缺口"


def _topic(**overrides) -> dict:
    body = {"kind": "topic", "text": TEXT, "labels": LABELS}
    body.update(overrides)
    return body


def _labels(n: int) -> list[dict]:
    return [{"key": f"k{i}", "label": f"領域{i}", "description": ""} for i in range(n)]


def test_a_topic_job_needs_no_url(kit):
    client, *_ = kit
    response = client.post("/jobs", json=_topic())
    assert response.status_code == 202
    body = response.json()
    assert body["kind"] == "topic"
    assert body["url"] == ""


def test_a_topic_job_carries_its_text_and_labels_to_the_worker(kit):
    client, store, *_ = kit
    job_id = client.post("/jobs", json=_topic()).json()["id"]
    job = store.get(job_id)
    assert job.text == TEXT
    assert job.labels == (
        TopicLabel("defense", "國防外交", "國防、軍事、外交、兩岸、僑務"),
        TopicLabel("welfare", "衛生福利", "醫療、健保、長照、社福、托育、食安"),
    )


def test_an_old_client_that_sends_no_kind_still_gets_a_deck_job(kit):
    """攔的 bug：新聞服務的摘要排程不會因為 GPU 端升級而改送 kind；
    沒帶 kind 必須照舊是摘要工作。"""
    client, store, *_ = kit
    body = client.post("/jobs", json={"url": IVOD}).json()
    assert body["kind"] == "deck"
    assert store.get(body["id"]).kind == JobKind.DECK


def test_the_listing_shows_each_jobs_kind(kit):
    client, *_ = kit
    client.post("/jobs", json={"url": IVOD})
    client.post("/jobs", json=_topic())
    assert [item["kind"] for item in client.get("/jobs").json()] == ["topic", "deck"]


def test_an_unknown_kind_is_refused(kit):
    client, *_ = kit
    assert client.post("/jobs", json=_topic(kind="speech")).status_code == 422


@pytest.mark.parametrize("text", ["", "   \n"])
def test_a_topic_job_without_text_is_refused(kit, text):
    client, *_ = kit
    assert client.post("/jobs", json=_topic(text=text)).status_code == 422


def test_a_topic_job_that_omits_the_text_field_is_refused(kit):
    client, *_ = kit
    body = _topic()
    del body["text"]
    assert client.post("/jobs", json=body).status_code == 422


def test_topic_text_is_capped_at_4000_characters(kit):
    """擋的是誤把整份逐字稿送進來的請求：準確率是在短輸入上量的，長輸入
    分出來的結果沒有人評估過。中文一個字算一個。"""
    client, *_ = kit
    assert client.post("/jobs", json=_topic(text="字" * 4000)).status_code == 202
    assert client.post("/jobs", json=_topic(text="字" * 4001)).status_code == 422


@pytest.mark.parametrize("count,status", [(0, 422), (1, 422), (2, 202), (20, 202),
                                          (21, 422)])
def test_a_topic_job_needs_two_to_twenty_labels(kit, count, status):
    client, *_ = kit
    assert client.post("/jobs", json=_topic(labels=_labels(count))).status_code == status


def test_duplicate_label_keys_are_refused(kit):
    client, *_ = kit
    labels = [{"key": "a", "label": "甲"}, {"key": "a", "label": "乙"}]
    assert client.post("/jobs", json=_topic(labels=labels)).status_code == 422


def test_duplicate_label_names_are_refused(kit):
    """模型選的是名稱，再由名稱對回 key：名稱重複就對不回唯一的 key。"""
    client, *_ = kit
    labels = [{"key": "a", "label": "甲"}, {"key": "b", "label": "甲"}]
    assert client.post("/jobs", json=_topic(labels=labels)).status_code == 422


def test_a_label_without_a_key_or_a_name_is_refused(kit):
    client, *_ = kit
    no_key = [{"key": "", "label": "甲"}, {"key": "b", "label": "乙"}]
    no_name = [{"key": "a", "label": ""}, {"key": "b", "label": "乙"}]
    assert client.post("/jobs", json=_topic(labels=no_key)).status_code == 422
    assert client.post("/jobs", json=_topic(labels=no_name)).status_code == 422


def test_a_label_whose_key_or_name_is_only_whitespace_is_refused(kit):
    """攔的 bug：min_length 只看長度，"  " 會過。只有空白的名稱去掉空白就是空的，
    對回代碼時會跟「模型回 null」混在一起；只有空白的 key 存進資料庫也認不出來。"""
    client, *_ = kit
    blank_key = [{"key": "  ", "label": "甲"}, {"key": "b", "label": "乙"}]
    blank_name = [{"key": "a", "label": " \n"}, {"key": "b", "label": "乙"}]
    assert client.post("/jobs", json=_topic(labels=blank_key)).status_code == 422
    assert client.post("/jobs", json=_topic(labels=blank_name)).status_code == 422


@pytest.mark.parametrize("body", [{}, {"kind": "deck"}, {"kind": "deck", "text": TEXT,
                                                        "labels": LABELS}])
def test_a_deck_job_still_requires_a_url(kit, body):
    """分類工作可以不帶網址，不代表摘要工作也可以：沒有網址就沒有影片。"""
    client, *_ = kit
    assert client.post("/jobs", json=body).status_code == 422


def test_a_topic_job_may_carry_a_url_but_only_an_http_one(kit):
    """分類用不到網址，帶了只是讓工作清單看得出是哪一篇。但帶了就照摘要
    工作的規矩：哪天有人把網址拿去用，file:// 不能是現成的漏洞。"""
    client, *_ = kit
    assert client.post("/jobs", json=_topic(url=IVOD)).status_code == 202
    assert client.post("/jobs", json=_topic(url="file:///C:/Windows/win.ini")).status_code == 422


# --- 追問工作（kind=followup）---

REQUEST = "要求衛福部一個月內提出長照人力補助方案"
CARD = "一句話：追問長照人力補助方案進度\n各段小標：\n- 長照人力缺口"
EXCERPT = "委員：上次要求的長照人力補助方案到現在還沒看到。"


def _followup(**overrides) -> dict:
    body = {"kind": "followup", "request": REQUEST, "response": "部長允諾一個月內提出",
            "card": CARD, "excerpt": EXCERPT}
    body.update(overrides)
    return body


def _without(body: dict, field: str) -> dict:
    return {k: v for k, v in body.items() if k != field}


def test_a_followup_job_needs_no_url(kit):
    client, *_ = kit
    response = client.post("/jobs", json=_followup())
    assert response.status_code == 202
    assert (response.json()["kind"], response.json()["url"]) == ("followup", "")


def test_a_followup_job_carries_its_pair_to_the_worker(kit):
    client, store, *_ = kit
    job_id = client.post("/jobs", json=_followup()).json()["id"]
    assert store.get(job_id).followup == FollowUpPair(
        request=REQUEST, response="部長允諾一個月內提出", card=CARD, excerpt=EXCERPT)


def test_deck_and_topic_jobs_carry_no_pair(kit):
    """摘要與分類工作不會多帶一份用不到的判斷內容，送了也一樣。"""
    client, store, *_ = kit
    deck = client.post("/jobs", json={"url": IVOD, "request": REQUEST, "card": CARD}).json()
    topic = client.post("/jobs", json=_topic(request=REQUEST, card=CARD)).json()
    assert store.get(deck["id"]).followup is None
    assert store.get(topic["id"]).followup is None


@pytest.mark.parametrize("field", ["request", "card", "excerpt"])
@pytest.mark.parametrize("value", ["", "  \n"])
def test_a_followup_job_without_a_request_a_card_or_an_excerpt_is_refused(kit, field, value):
    """沒有舊的要求就沒有東西可追；沒有新文章的摘要卡，模型只能憑一段逐字稿猜。

    攔的 bug：沒有逐字稿片段也收的話，模型找不到能抄的證據、照提示判成「沒有」，
    新聞服務把那一對記成判過、之後不再判——一個沒看過證據的「沒有追問」就這樣
    算進追問率，而且不會有任何地方報錯。"""
    client, store, *_ = kit
    response = client.post("/jobs", json=_followup(**{field: value}))
    assert response.status_code == 422
    assert field in response.json()["detail"]
    assert store.recent_snapshots() == []


@pytest.mark.parametrize("field", ["request", "card", "excerpt"])
def test_a_followup_job_that_omits_a_required_field_is_refused(kit, field):
    client, *_ = kit
    assert client.post("/jobs", json=_without(_followup(), field)).status_code == 422


def test_the_response_may_be_left_out(kit):
    """官員當場可能沒回應——那一對照樣要判。"""
    client, store, *_ = kit
    response = client.post("/jobs", json=_without(_followup(), "response"))
    assert response.status_code == 202
    assert store.get(response.json()["id"]).followup.response == ""


def test_the_excerpt_is_capped_at_1500_characters(kit):
    """新聞服務挑的是一段視窗，不是整份逐字稿；長輸入判出來的結果沒有人評估過。"""
    client, *_ = kit
    assert client.post("/jobs", json=_followup(excerpt="字" * 1500)).status_code == 202
    assert client.post("/jobs", json=_followup(excerpt="字" * 1501)).status_code == 422


@pytest.mark.parametrize("field,limit", [("request", 500), ("response", 500),
                                         ("card", 4000)])
def test_the_other_fields_are_capped_too(kit, field, limit):
    """擋的是誤送整份逐字稿的請求，不是正常的要求或摘要卡（那些只有幾十到幾百字）。"""
    client, *_ = kit
    assert client.post("/jobs", json=_followup(**{field: "字" * limit})).status_code == 202
    assert client.post("/jobs", json=_followup(**{field: "字" * (limit + 1)})).status_code == 422


def test_a_followup_job_may_carry_a_url_but_only_an_http_one(kit):
    client, *_ = kit
    assert client.post("/jobs", json=_followup(url=IVOD)).status_code == 202
    assert client.post("/jobs", json=_followup(url="file:///C:/Windows/win.ini")).status_code == 422


def test_the_listing_shows_followup_jobs_by_kind(kit):
    client, *_ = kit
    client.post("/jobs", json={"url": IVOD})
    client.post("/jobs", json=_topic())
    client.post("/jobs", json=_followup())
    assert [item["kind"] for item in client.get("/jobs").json()] == ["followup", "topic", "deck"]


def test_a_deck_job_with_followup_fields_still_requires_a_url(kit):
    """判斷工作可以不帶網址，不代表摘要工作也可以。"""
    client, *_ = kit
    body = _without(_followup(), "kind")
    assert client.post("/jobs", json=body).status_code == 422


def test_a_topic_job_is_validated_as_before_even_with_followup_fields(kit):
    client, *_ = kit
    assert client.post("/jobs", json=_topic(text="", request=REQUEST, card=CARD)).status_code == 422


def test_a_followup_job_round_trips_from_post_to_result(monkeypatch):
    """從 POST 到 GET 走一遍真正的分派、執行函式、Ollama 判斷器與成品格式，只把
    送給 Ollama 的那一個 HTTP 請求換掉：新聞服務讀的就是這一份 JSON。"""
    reply = json.dumps({"followed_up": True, "quote": " 上次要求的長照人力補助方案到現在還沒看到 "},
                       ensure_ascii=False)
    sent = {}

    def fake_post(url, body, timeout):
        sent["body"] = body
        line = json.dumps({"message": {"content": reply}, "done": True}, ensure_ascii=False)
        return io.BytesIO(line.encode("utf-8") + b"\n")

    monkeypatch.setattr(ollama_summarizer, "_post_json", fake_post)
    store = JobStore()
    judge = FollowUpExecutor(partial(OllamaFollowUpJudge, "http://gpu:11434", num_ctx=32768),
                             "qwen3.5:9b")
    worker = JobWorker(store, ByKindExecutor({JobKind.FOLLOWUP: judge}))
    with TestClient(_app(store, worker)) as client:
        worker.stop()
        job_id = client.post("/jobs", json=_followup()).json()["id"]
        worker.run_once()
        body = client.get(f"/jobs/{job_id}").json()
    assert body["status"] == "done"
    assert body["result"] == {"followed_up": True,
                              "quote": "上次要求的長照人力補助方案到現在還沒看到",
                              "classifier": followup_classifier("qwen3.5:9b")}
    assert EXCERPT in sent["body"]["messages"][1]["content"]
