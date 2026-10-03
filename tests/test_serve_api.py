"""摘要 API 的組裝：每一種工作都真的接上了執行函式，不碰 Ollama 也不碰網路。

serve_api.py 一 import 就會組好整個服務（不啟動工作執行緒、不載入語音模型），
所以這裡直接拿它的 build_executor 來測，只把送給 Ollama 的那一個 HTTP 請求換掉。
"""
from __future__ import annotations

import io
import json

import pytest

import serve_api
from slidebox.domain.entities import FollowUpPair, Settings
from slidebox.infrastructure import ollama_summarizer
from slidebox.usecases.followups import classifier_name
from slidebox_api.jobs import JobKind, JobStore

PAIR = FollowUpPair(request="要求一個月內提出長照人力補助方案", response="部長允諾",
                    card="一句話：追問長照人力補助方案", excerpt="上次要求的方案還沒看到")


@pytest.fixture
def sent(monkeypatch):
    record = {}

    def fake_post(url, body, timeout):
        record.update(url=url, body=body)
        reply = json.dumps({"followed_up": True, "quote": "上次要求的方案還沒看到"},
                           ensure_ascii=False)
        line = json.dumps({"message": {"content": reply}, "done": True}, ensure_ascii=False)
        return io.BytesIO(line.encode("utf-8") + b"\n")

    monkeypatch.setattr(ollama_summarizer, "_post_json", fake_post)
    return record


def _settings(tmp_path) -> Settings:
    return Settings(output_dir=str(tmp_path), model="qwen3.5:9b",
                    ollama_host="http://gpu:11434", num_ctx=16384)


@pytest.mark.parametrize("kind", list(JobKind))
def test_every_kind_of_job_has_an_executor(tmp_path, kind):
    """攔的 bug：新增一種工作、API 也收了，組裝時卻忘了接上——每一個這種工作都
    會在 GPU 端失敗成「不支援的工作種類」，而單元測試全部是綠的。"""
    executor = serve_api.build_executor(_settings(tmp_path))
    assert kind in executor._executors


def test_a_followup_job_runs_with_the_services_model_host_and_context(tmp_path, sent):
    """判斷器用的是服務的預設模型、Ollama 位址與跟摘要同一個 num_ctx，成品上的
    判斷器名稱也是那個模型的。"""
    executor = serve_api.build_executor(_settings(tmp_path))
    job = JobStore().submit(kind=JobKind.FOLLOWUP, followup=PAIR)
    result = executor(job, lambda fraction, status: None, lambda: False)
    assert result == {"followed_up": True, "quote": "上次要求的方案還沒看到",
                      "classifier": classifier_name("qwen3.5:9b")}
    assert sent["url"] == "http://gpu:11434/api/chat"
    assert sent["body"]["model"] == "qwen3.5:9b"
    assert sent["body"]["options"]["num_ctx"] == 16384
