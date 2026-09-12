"""工作佇列：純狀態機，不碰 HTTP 也不碰 slidebox。"""
from __future__ import annotations

import pytest

from slidebox_api.jobs import JobStatus, JobStore


def _store() -> JobStore:
    return JobStore()


def test_a_submitted_job_starts_queued_and_gets_an_id():
    store = _store()
    job = store.submit("https://ivod.ly.gov.tw/Play/Clip/1M/1", detailed=True)
    assert job.status == JobStatus.QUEUED
    assert job.id
    assert store.get(job.id) is job


def test_ids_are_unique():
    store = _store()
    ids = {store.submit(f"u{i}").id for i in range(20)}
    assert len(ids) == 20


def test_jobs_are_taken_in_the_order_they_arrived():
    """使用者排的順序就是他要的順序——這與桌面 app 的佇列同一個道理。"""
    store = _store()
    first = store.submit("a")
    second = store.submit("b")
    assert store.take_next() is first
    store.finish(first.id, {"ok": True})
    assert store.take_next() is second


def test_only_one_job_runs_at_a_time():
    """攔的 bug：兩個生成同時跑會互搶 Ollama 與 ffmpeg，GPU 只有一張卡。"""
    store = _store()
    store.submit("a")
    store.submit("b")
    assert store.take_next() is not None
    assert store.take_next() is None


def test_taking_a_job_marks_it_running_and_records_when():
    store = _store()
    job = store.submit("a")
    store.take_next()
    assert job.status == JobStatus.RUNNING
    assert job.started_at is not None


def test_progress_is_visible_while_the_job_runs():
    store = _store()
    job = store.submit("a")
    store.take_next()
    store.progress(job.id, 0.4, "產生摘要…")
    assert job.progress_fraction == pytest.approx(0.4)
    assert job.progress_status == "產生摘要…"


def test_finishing_stores_the_result_and_frees_the_slot():
    store = _store()
    job = store.submit("a")
    store.take_next()
    store.finish(job.id, {"slides": []})
    assert job.status == JobStatus.DONE
    assert job.result == {"slides": []}
    assert job.finished_at is not None
    assert store.running is None


def test_failing_keeps_the_reason_and_frees_the_slot():
    store = _store()
    job = store.submit("a")
    store.take_next()
    store.fail(job.id, "連不上 Ollama")
    assert job.status == JobStatus.FAILED
    assert "Ollama" in job.error
    assert store.running is None


def test_a_queued_job_can_be_cancelled_without_ever_running():
    store = _store()
    first = store.submit("a")
    second = store.submit("b")
    assert store.cancel(second.id) is True
    store.take_next()
    store.finish(first.id, {})
    assert store.take_next() is None


def test_cancelling_a_running_job_raises_its_flag_rather_than_killing_it():
    """攔的 bug：直接把狀態改成已取消，工作執行緒還在跑，接著它會把結果
    寫進一個已經結束的 job，而 GPU 也還被佔著。"""
    store = _store()
    job = store.submit("a")
    store.take_next()
    assert store.cancel(job.id) is True
    assert job.cancel_requested.is_set()
    assert job.status == JobStatus.RUNNING
    assert store.running is job


def test_cancelling_an_unknown_job_says_so():
    assert _store().cancel("沒這個 id") is False


def test_recent_lists_newest_first_and_is_capped():
    store = _store()
    for i in range(5):
        store.submit(f"u{i}")
    recent = store.recent_snapshots(limit=3)
    assert [j.url for j in recent] == ["u4", "u3", "u2"]


def test_a_snapshot_never_shows_done_without_its_result():
    """攔的 bug：狀態先寫、結果後寫，而讀取端在鎖外逐一取屬性——讀到那個
    瞬間的話，Pi 會判定工作失敗，幾分鐘的 GPU 成品就報銷了。"""
    store = _store()
    job = store.submit("a")
    store.take_next()
    store.finish(job.id, {"slides": []})
    snapshot = store.snapshot(job.id)
    assert snapshot.status == JobStatus.DONE
    assert snapshot.result == {"slides": []}


def test_a_snapshot_does_not_change_under_the_readers_feet():
    store = _store()
    job = store.submit("a")
    snapshot = store.snapshot(job.id)
    store.take_next()
    assert snapshot.status == JobStatus.QUEUED


def test_old_finished_jobs_are_forgotten_so_memory_does_not_grow_forever():
    """這個服務會連續跑好幾個月。每篇成品帶著 base64 圖片，全部留著會把
    記憶體吃光。"""
    store = JobStore(max_jobs=3)
    ids = []
    for i in range(5):
        job = store.submit(f"u{i}")
        store.take_next()
        store.finish(job.id, {"big": "x" * 1000})
        ids.append(job.id)
    assert store.get(ids[0]) is None
    assert store.get(ids[-1]) is not None


def test_a_running_job_is_never_forgotten_even_when_the_cap_is_reached():
    store = JobStore(max_jobs=2)
    job = store.submit("a")
    store.take_next()
    for i in range(5):
        store.submit(f"u{i}")
    assert store.get(job.id) is job
