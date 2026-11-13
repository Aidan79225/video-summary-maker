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


# --- 多人說話者區分的組裝 ---

FAKE_TOKEN = "hf_FAKEtokenForTestsOnly0123456789"


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """服務在組裝時讀環境變數：先清掉開發機上可能有的設定，測試才不看機器。"""
    for name in ("HUGGINGFACE_TOKEN", "SLIDEBOX_DIARIZER", "SLIDEBOX_DIARIZATION_DEVICE",
                 "SLIDEBOX_CHUNK_CHARS", "SLIDEBOX_QUOTE_ONLY", "SLIDEBOX_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SLIDEBOX_API_OUTPUT_DIR", str(tmp_path))
    return monkeypatch


def _deck_usecase(executor):
    return executor._executors[JobKind.DECK]._usecase


def test_the_chunk_size_comes_from_the_environment(tmp_path, clean_env):
    from slidebox.usecases.conversation import DEFAULT_CHUNK_CHARS

    assert _deck_usecase(serve_api.build_executor(_settings(tmp_path)))._chunk_chars == DEFAULT_CHUNK_CHARS
    clean_env.setenv("SLIDEBOX_CHUNK_CHARS", "3000")
    assert _deck_usecase(serve_api.build_executor(_settings(tmp_path)))._chunk_chars == 3000


@pytest.mark.parametrize("raw", ["abc", "", "999", "20001", "-5", "3000.5"])
def test_a_chunk_size_that_makes_no_sense_means_the_default(tmp_path, clean_env, raw):
    """攔的 bug：打錯一個環境變數就讓每一支多人影片切出怪窗（太小＝模型沒有上下文，太大＝塞爆 num_ctx）。"""
    from slidebox.usecases.conversation import DEFAULT_CHUNK_CHARS

    clean_env.setenv("SLIDEBOX_CHUNK_CHARS", raw)
    assert _deck_usecase(serve_api.build_executor(_settings(tmp_path)))._chunk_chars == DEFAULT_CHUNK_CHARS


def test_the_safe_mode_is_off_unless_the_environment_turns_it_on(tmp_path, clean_env):
    assert _deck_usecase(serve_api.build_executor(_settings(tmp_path)))._quote_only is False
    clean_env.setenv("SLIDEBOX_QUOTE_ONLY", "1")
    assert _deck_usecase(serve_api.build_executor(_settings(tmp_path)))._quote_only is True


@pytest.mark.parametrize("raw, expected", [
    ("1", True), ("true", True), ("TRUE", True), ("Yes", True), (" yes ", True),
    ("0", False), ("false", False), ("no", False), ("", False), ("on", False), ("2", False), ("enable", False),
])
def test_the_safe_mode_switch_only_accepts_1_true_and_yes(tmp_path, clean_env, raw, expected):
    """攔的 bug：打錯字（on、enable）被當成「開」而悄悄改變每個重點的寫法，或寫了 true 卻沒有開。"""
    clean_env.setenv("SLIDEBOX_QUOTE_ONLY", raw)
    assert _deck_usecase(serve_api.build_executor(_settings(tmp_path)))._quote_only is expected


def test_the_capability_probe_is_the_very_diarizer_the_jobs_use(tmp_path, clean_env):
    """攔的 bug：/capabilities 與實際工作各建一個 diarizer——端點說「可以」，工作卻用另一個設定去跑。"""
    executor = serve_api.build_executor(_settings(tmp_path))
    assert executor.speaker_capabilities == _deck_usecase(executor)._diarizer.available


def test_without_a_token_the_service_says_so_and_still_serves_everything_else(clean_env):
    from fastapi.testclient import TestClient

    client = TestClient(serve_api.build())
    body = client.get("/capabilities").json()

    assert body["speaker_diarization"] is False
    assert "HUGGINGFACE_TOKEN" in body["reason"]
    assert body["speaker_contract"] == 1
    assert client.get("/health").json()["ok"] is True


def test_the_kill_switch_turns_the_capability_off_with_a_reason(clean_env):
    from fastapi.testclient import TestClient

    clean_env.setenv("HUGGINGFACE_TOKEN", FAKE_TOKEN)
    clean_env.setenv("SLIDEBOX_DIARIZER", "off")
    body = TestClient(serve_api.build()).get("/capabilities").json()
    assert body["speaker_diarization"] is False and "SLIDEBOX_DIARIZER=off" in body["reason"]


def test_the_token_never_appears_in_anything_the_service_says_about_itself(clean_env):
    from fastapi.testclient import TestClient

    clean_env.setenv("HUGGINGFACE_TOKEN", FAKE_TOKEN)
    client = TestClient(serve_api.build())
    for path in ("/capabilities", "/health", "/jobs"):
        assert FAKE_TOKEN not in client.get(path).text, path
