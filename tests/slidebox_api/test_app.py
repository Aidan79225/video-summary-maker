"""HTTP 介面：用假的執行函式，不碰 Ollama 也不碰網路。"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from slidebox.domain.errors import OperationCancelled
from slidebox_api.app import create_app
from slidebox_api.jobs import JobStatus, JobStore
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
