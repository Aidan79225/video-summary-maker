"""HTTP 介面：用假的執行函式，不碰 Ollama 也不碰網路。"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from slidebox.domain.entities import TopicLabel
from slidebox.domain.errors import OperationCancelled
from slidebox_api.app import create_app
from slidebox_api.jobs import JobKind, JobStatus, JobStore
from slidebox_api.runner import JobWorker

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
